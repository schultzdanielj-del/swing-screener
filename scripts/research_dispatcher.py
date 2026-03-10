"""
Research Dispatcher — Phase-based autonomous research sessions.

TOKEN-EFFICIENT DESIGN:
  Claude Code only runs when there's thinking to do. During grind waits
  (15-20 min each), zero tokens are consumed. The dispatcher handles all
  polling and orchestration in plain Python.

FLOW:
  Phase 1: PLAN — Claude Code reads the repo, forms a research plan, writes it to a JSON file
  Phase 2+: EXECUTE — For each step in the plan, Claude Code runs, does the work, exits
  Between phases: Dispatcher waits for any queued grinds to complete (zero tokens)
  Final phase: SUMMARIZE — Claude Code writes RESEARCH_FINDINGS.md

MODEL: Sonnet (--model sonnet) — good enough for research, much lighter on usage budget

Usage:
    python scripts/research_dispatcher.py

Post jobs from anywhere:
    POST https://web-production-e3025.up.railway.app/api/v2/research
    {"prompt": "Find a way to reduce signal count without losing edge"}

Hard rails:
    - All work on sandbox branch (v2 untouched)
    - Examples benched, not deleted (reversible)
    - Max 8 phases per job (prevents runaway)
    - Max 2 hours total per job
    - Sonnet model (not Opus) to conserve usage
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
POLL_INTERVAL = 15       # seconds between job polls
GRIND_POLL = 30          # seconds between grind completion polls
MAX_PHASES = 8           # default, overridden per job from server
MAX_TOTAL_TIME = 14400   # default, overridden per job from server
MODEL = "opus"            # opus follows complex instructions reliably

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
            data = r.json()
            jobs = data.get("jobs", [])
            if jobs:
                log(f"Found job: #{jobs[0].get('id')} status={jobs[0].get('status')}")
            return jobs[0] if jobs else None
        else:
            log(f"Pending endpoint returned {r.status_code}")
    except Exception as e:
        log(f"Poll error: {e}")
    return None


def create_sandbox_branch(job_id):
    """Create a sandbox branch from v2."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    branch = f"research/{ts}_job{job_id}"
    subprocess.run(["git", "checkout", "v2"], cwd=REPO_ROOT, capture_output=True)
    subprocess.run(["git", "pull"], cwd=REPO_ROOT, capture_output=True)
    # Delete branch if it exists from a previous failed run
    subprocess.run(["git", "branch", "-D", branch], cwd=REPO_ROOT, capture_output=True)
    subprocess.run(["git", "checkout", "-b", branch], cwd=REPO_ROOT, capture_output=True)
    return branch


def cleanup_sandbox(branch):
    """Switch back to v2. Keep branch for review."""
    subprocess.run(["git", "checkout", "v2"], cwd=REPO_ROOT, capture_output=True)


def get_diff(branch):
    """Get diff between v2 and research branch."""
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
    if len(full_diff) > 50000:
        full_diff = full_diff[:50000] + "\n\n... (truncated)"
    return f"{stat}\n\n{full_diff}"


def find_claude_cmd():
    """Find working Claude Code CLI command."""
    is_win = sys.platform == "win32"
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
                return cmd
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def run_claude_code(prompt, system_prompt, phase_name=""):
    """Run a single Claude Code invocation. Returns (output, exit_code)."""
    is_win = sys.platform == "win32"
    claude_cmd = find_claude_cmd()
    if not claude_cmd:
        return "ERROR: Claude Code CLI not found", 1

    if claude_cmd == "npx":
        cli_args = ["npx", "@anthropic-ai/claude-code",
                     "-p", prompt,
                     "--model", MODEL,
                     "--system-prompt", system_prompt,
                     "--dangerously-skip-permissions"]
    else:
        cli_args = [claude_cmd,
                     "-p", prompt,
                     "--model", MODEL,
                     "--system-prompt", system_prompt,
                     "--dangerously-skip-permissions"]

    log(f"  Claude Code ({phase_name})...")

    proc = subprocess.Popen(
        cli_args, cwd=REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        encoding='utf-8', errors='replace',
        shell=is_win,
    )

    output_lines = []
    for line in proc.stdout:
        line = line.rstrip()
        print(f"      {line}")
        output_lines.append(line)

    proc.wait()
    output = "\n".join(output_lines)
    log(f"  Claude Code ({phase_name}) exit={proc.returncode}")
    return output, proc.returncode


def read_file_safe(path):
    """Read a file, return empty string if not found."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""


# ── System prompts per phase ─────────────────────────────────────────

PLAN_SYSTEM = """You are a senior quant researcher working on a swing trading screener.
You're on a sandbox git branch. Production code (v2) is untouched.

YOUR JOB: Read the codebase, understand the problem, and create a research plan.

OUTPUT: Write a JSON file at data/research_plan.json with this structure:
{
  "summary": "One paragraph describing your overall approach",
  "steps": [
    {
      "id": 1,
      "action": "what to do",
      "type": "code_change" | "run_grind" | "run_script" | "analyze",
      "grind_command": "signal_grind" (only if type=run_grind),
      "grind_args": {},
      "description": "why this step matters",
      "depends_on_grind": false
    }
  ]
}

IMPORTANT RULES:
- Each step should be a discrete unit of work
- If a step needs grind results, set type="run_grind" — the dispatcher will queue it and wait
- Steps after a grind should reference the grind result
- Max """ + str(MAX_PHASES) + """ steps total (including plan and summarize phases, so ~6 work steps)
- Don't execute anything yet — just plan
- Read these files first: PIPELINE_V2.md, ta_knowledge.md, local_runner/pyramid_grinder.py, local_runner/spiderweb.py

THE SYSTEM:
  The pyramid grinder takes example trades and finds math conditions separating them from
  the full 4,167-ticker universe. Tiers: D1->5yr, adding conditions at each level.
  Constraint: 100% examples pass all conditions. More examples = wider ranges = more signals.
  Current problem: ~69 examples, signal count ~1,200+. Need it under 500.

GRIND RESULTS are on Railway:
  List: curl -s \"""" + API_BASE + """/api/v2/files?prefix=local_runner/cache/pyramid_dtss"
  Get:  curl -s \"""" + API_BASE + """/api/v2/files/{path}"

EXAMPLES:
  List: curl -s \"""" + API_BASE + """/api/examples/dtss"

KEY FILES: PIPELINE_V2.md, ta_knowledge.md, local_runner/pyramid_grinder.py,
  local_runner/spiderweb.py, scripts/example_outlier_analysis.py

IDEAS TO CONSIDER (but don't limit yourself):
  - Condition selection: is greedy peak-reduction optimal? Information gain?
  - Tier allocation: D1 fewer/more conditions? Weekly/monthly more?
  - Scoring: peak/day vs median/day vs something else
  - Range computation: 5% margin — adaptive margins?
  - Expression weighting: some are noise
  - Condition interaction: redundant conditions?
  - Post-grind pruning: remove conditions not filtering much
  - Multi-objective: minimize signals while maximizing separation
  - Example clustering: subgroups needing different conditions?
  - The junk expression problem: 58% have >95% universe pass rate

Commit the plan file when done.
"""


EXECUTE_SYSTEM_TEMPLATE = """You are a senior quant researcher. You're on a sandbox git branch.

You are executing ONE STEP of a research plan. Do the work described, commit changes,
and write your results to data/research_step_{step_id}_result.json with:
{{
  "step_id": {step_id},
  "status": "done" | "failed",
  "result": "what happened",
  "signal_count": null or integer,
  "files_changed": ["list of files"],
  "queued_task_id": null or integer (if you queued a grind via task queue)
}}

ENVIRONMENT:
- Repo root: """ + REPO_ROOT + """
- All grind results on Railway: """ + API_BASE + """
- Task queue for grinds: POST """ + API_BASE + """/api/v2/tasks with {{"command":"signal_grind","args":{{"setup":"dtss"}}}}
- Poll task: GET """ + API_BASE + """/api/v2/tasks/{{id}}
- DO NOT poll/wait for grind results. Queue the grind and exit. The dispatcher waits.

EXAMPLES (bench, don't delete):
  To bench: GET example data, save to data/research_bench.json, then DELETE /api/examples/dtss/{{id}}
  
RULES:
- Do the work for THIS step only
- Commit to the sandbox branch
- Don't touch v2 branch
- Don't rebuild expression cache or OHLCV cache
- If you need to queue a grind, do it and exit immediately — don't wait
"""


SUMMARIZE_SYSTEM = """You are a senior quant researcher. You've completed a research session.

Read all step result files (data/research_step_*_result.json) and the plan (data/research_plan.json).
Check git log for what changed.

Write RESEARCH_FINDINGS.md in the repo root with:
1. Executive summary — bottom line, what worked and what didn't
2. Each experiment — what was tried, why, the result (signal counts, condition counts)  
3. Code changes — what was written/modified and why
4. Recommendations — what Dan should merge to v2, what to discard
5. Benched examples — list with reasoning, how to restore
6. Next steps — what to try next based on what was learned

Check data/research_bench.json for benched examples and include restoration instructions.

Commit RESEARCH_FINDINGS.md to the sandbox branch.

Dan is sleeping. He'll read this in the morning. Make it worth waking up to.

ENVIRONMENT:
- Grind results on Railway: """ + API_BASE + """
- Repo root: """ + REPO_ROOT + """
"""


# ── Main job runner ──────────────────────────────────────────────────

def run_research_job(job):
    """Execute a research job in phases."""
    job_id = job["id"]
    prompt = job["prompt"]
    max_time = job.get("max_time_s") or MAX_TOTAL_TIME
    max_phases = job.get("max_phases") or MAX_PHASES
    job_start = time.time()
    all_logs = []

    log(f"{'='*60}")
    log(f"Research Job #{job_id}")
    log(f"Prompt: {prompt}")
    log(f"Max time: {max_time}s, Max phases: {max_phases}")
    log(f"{'='*60}")

    # Setup
    branch = create_sandbox_branch(job_id)
    log(f"Branch: {branch}")
    update_job(job_id, status="running", branch=branch)

    try:
        # ── Phase 1: PLAN ──
        log("Phase 1: PLAN")
        plan_prompt = f"""RESEARCH JOB #{job_id}

DO NOT ASK QUESTIONS. DO NOT REQUEST CLARIFICATION. EXECUTE IMMEDIATELY.

DAN'S RESEARCH PROMPT:
{prompt}

YOUR TASK RIGHT NOW:
1. Read these files in the repo: PIPELINE_V2.md, local_runner/pyramid_grinder.py, local_runner/spiderweb.py, ta_knowledge.md
2. Based on what you learn and Dan's prompt above, create a research plan
3. Write the plan to data/research_plan.json (create the data/ directory if needed)
4. git add and git commit the plan file

The plan JSON format:
{{
  "summary": "One paragraph describing your approach",
  "steps": [
    {{
      "id": 1,
      "action": "description of what to do",
      "type": "code_change",
      "description": "why this matters"
    }}
  ]
}}

Step types: "code_change" (modify grinder code), "run_grind" (with "grind_command": "signal_grind", "grind_args": {{"setup": "dtss"}}), "run_script", "analyze"

CONTEXT (so you don't need network access):
- 69 DTSS examples in the database
- Latest grind: 1292 signals, 16 peak/day, 82 conditions
- Tier breakdown: D1=15, 1wk=15, 1mo=20, 6mo=8, 1yr=2, 5yr=6, weekly=12, monthly=4
- D1 is capped at 15 conditions
- Expressions: 15,805 total, 58% have >95% universe pass rate (junk)
- Condition ranges set by min/max of example values + 5% margin
- Grinder uses greedy peak-reduction scoring in beam search
- 100% example pass rate is non-negotiable constraint

DO NOT ASK WHAT TO DO. THE PROMPT ABOVE IS YOUR ASSIGNMENT. START READING FILES NOW."""

        output, code = run_claude_code(plan_prompt, PLAN_SYSTEM, "PLAN")
        all_logs.append(f"=== PHASE 1: PLAN (exit={code}) ===\n{output}\n")

        if code != 0:
            update_job(job_id, status="failed", error=f"Plan phase failed (exit {code})",
                       log="\n".join(all_logs)[:100000])
            cleanup_sandbox(branch)
            return

        # Read the plan
        plan_path = os.path.join(REPO_ROOT, "data", "research_plan.json")
        plan_text = read_file_safe(plan_path)
        if not plan_text:
            update_job(job_id, status="failed", error="No research_plan.json created",
                       log="\n".join(all_logs)[:100000])
            cleanup_sandbox(branch)
            return

        try:
            plan = json.loads(plan_text)
            steps = plan.get("steps", [])
        except json.JSONDecodeError as e:
            update_job(job_id, status="failed", error=f"Invalid plan JSON: {e}",
                       log="\n".join(all_logs)[:100000])
            cleanup_sandbox(branch)
            return

        log(f"Plan: {len(steps)} steps")
        for s in steps:
            log(f"  Step {s['id']}: [{s.get('type','')}] {s.get('action','')[:60]}")

        # ── Phase 2+: EXECUTE steps ──
        phase_num = 2
        for step in steps:
            # Time check
            elapsed = time.time() - job_start
            if elapsed > max_time:
                log(f"Max time ({max_time}s) exceeded — stopping")
                break
            if phase_num > max_phases:
                log(f"Max phases ({max_phases}) reached — stopping")
                break

            step_id = step["id"]
            step_type = step.get("type", "code_change")
            log(f"Phase {phase_num}: Step {step_id} [{step_type}] — {step.get('action', '')[:60]}")

            # If this is a grind step, queue it via API and wait (zero tokens)
            if step_type == "run_grind":
                grind_cmd = step.get("grind_command", "signal_grind")
                grind_args = step.get("grind_args", {"setup": "dtss"})
                log(f"  Queuing grind: {grind_cmd} {grind_args}")

                try:
                    r = requests.post(f"{API_BASE}/api/v2/tasks",
                                      json={"command": grind_cmd, "args": grind_args}, timeout=15)
                    task_data = r.json()
                    task_id = task_data.get("id")
                    log(f"  Task #{task_id} queued — waiting (zero tokens)...")

                    # Wait for grind completion — NO TOKENS CONSUMED
                    while True:
                        elapsed = time.time() - job_start
                        if elapsed > max_time:
                            log("  Max time hit while waiting for grind")
                            break
                        try:
                            r2 = requests.get(f"{API_BASE}/api/v2/tasks/{task_id}", timeout=10)
                            status = r2.json().get("status", "")
                            if status in ("completed", "failed"):
                                log(f"  Task #{task_id}: {status}")
                                # Write result file for next phase
                                result_file = os.path.join(REPO_ROOT, "data", f"research_step_{step_id}_result.json")
                                os.makedirs(os.path.dirname(result_file), exist_ok=True)
                                with open(result_file, "w") as f:
                                    json.dump({
                                        "step_id": step_id,
                                        "status": "done" if status == "completed" else "failed",
                                        "result": f"Grind task #{task_id} {status}",
                                        "task_id": task_id,
                                    }, f, indent=2)
                                break
                            else:
                                log(f"  Task #{task_id}: {status}...")
                        except:
                            pass
                        time.sleep(GRIND_POLL)

                    all_logs.append(f"=== PHASE {phase_num}: GRIND {grind_cmd} (task #{task_id}) ===\n")

                except Exception as e:
                    log(f"  Failed to queue grind: {e}")
                    all_logs.append(f"=== PHASE {phase_num}: GRIND FAILED: {e} ===\n")

            else:
                # Code change / analysis / script — run Claude Code
                prev_results = []
                for prev_step in steps:
                    if prev_step["id"] >= step_id:
                        break
                    result_path = os.path.join(REPO_ROOT, "data", f"research_step_{prev_step['id']}_result.json")
                    prev_text = read_file_safe(result_path)
                    if prev_text:
                        prev_results.append(f"Step {prev_step['id']} result: {prev_text[:500]}")

                prev_context = "\n".join(prev_results) if prev_results else "No previous results yet."

                step_prompt = f"""DO NOT ASK QUESTIONS. EXECUTE IMMEDIATELY.

RESEARCH JOB #{job_id}, STEP {step_id}

ORIGINAL PROMPT FROM DAN:
{prompt}

THE STEP TO EXECUTE NOW:
{json.dumps(step, indent=2)}

PREVIOUS STEP RESULTS:
{prev_context}

INSTRUCTIONS:
1. Do the work described in the step above
2. Write results to data/research_step_{step_id}_result.json
3. git add and git commit your changes
4. If you need a grind, POST to {API_BASE}/api/v2/tasks and record the task ID in queued_task_id field

DO NOT ask for clarification. DO NOT explain what you would do. JUST DO IT."""

                system = EXECUTE_SYSTEM_TEMPLATE.replace("{step_id}", str(step_id))
                output, code = run_claude_code(step_prompt, system, f"STEP-{step_id}")
                all_logs.append(f"=== PHASE {phase_num}: STEP {step_id} (exit={code}) ===\n{output}\n")

                # Check if this step queued a grind
                result_path = os.path.join(REPO_ROOT, "data", f"research_step_{step_id}_result.json")
                result_text = read_file_safe(result_path)
                if result_text:
                    try:
                        result_data = json.loads(result_text)
                        queued_id = result_data.get("queued_task_id")
                        if queued_id:
                            log(f"  Step queued task #{queued_id} — waiting (zero tokens)...")
                            while True:
                                elapsed = time.time() - job_start
                                if elapsed > max_time:
                                    break
                                try:
                                    r = requests.get(f"{API_BASE}/api/v2/tasks/{queued_id}", timeout=10)
                                    status = r.json().get("status", "")
                                    if status in ("completed", "failed"):
                                        log(f"  Task #{queued_id}: {status}")
                                        break
                                    log(f"  Task #{queued_id}: {status}...")
                                except:
                                    pass
                                time.sleep(GRIND_POLL)
                    except json.JSONDecodeError:
                        pass

            phase_num += 1

        # ── Final phase: SUMMARIZE ──
        log(f"Final phase: SUMMARIZE")
        summary_prompt = f"""RESEARCH JOB #{job_id}

DAN'S ORIGINAL PROMPT:
{prompt}

You've completed a research session. Read all step results and write RESEARCH_FINDINGS.md.
Commit it to this branch."""

        output, code = run_claude_code(summary_prompt, SUMMARIZE_SYSTEM, "SUMMARIZE")
        all_logs.append(f"=== FINAL: SUMMARIZE (exit={code}) ===\n{output}\n")

        # ── Collect results ──
        findings = read_file_safe(os.path.join(REPO_ROOT, "RESEARCH_FINDINGS.md"))
        diff = get_diff(branch)
        bench = read_file_safe(os.path.join(REPO_ROOT, "data", "research_bench.json"))
        full_log = "\n".join(all_logs)

        duration = time.time() - job_start
        log(f"Job #{job_id} complete in {duration:.0f}s ({phase_num - 1} phases)")

        update_job(
            job_id,
            status="completed",
            summary=findings[:10000] if findings else f"Completed in {duration:.0f}s. Check branch {branch}.",
            diff=diff[:50000],
            examples_benched=bench[:5000] if bench else None,
            log=full_log[:100000],
        )

    except Exception as e:
        import traceback
        error_msg = f"{type(e).__name__}: {e}"
        log(f"Job #{job_id} error: {error_msg}")
        traceback.print_exc()
        update_job(job_id, status="failed", error=error_msg,
                   log="\n".join(all_logs)[:100000])

    finally:
        cleanup_sandbox(branch)


def main():
    print("\n" + "=" * 60)
    print("  RESEARCH DISPATCHER (phase-based, token-efficient)")
    print("=" * 60)
    print(f"  API:        {API_BASE}")
    print(f"  Model:      {MODEL}")
    print(f"  Max phases: {MAX_PHASES} per job")
    print(f"  Max time:   {MAX_TOTAL_TIME}s per job")
    print(f"  Poll:       every {POLL_INTERVAL}s")
    print(f"  Repo:       {REPO_ROOT}")
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
