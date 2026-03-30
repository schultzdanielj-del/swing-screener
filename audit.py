"""
ScanPerfect Code Auditor
Run from any terminal: python audit.py
Diffs from last audited commit to current HEAD.
Uses DEPENDENCY_MAP.md to check downstream consumers.
"""

import os
import re
import subprocess
import sys

REPO_ROOT = subprocess.check_output(
    ["git", "rev-parse", "--show-toplevel"], text=True
).strip()

STATE_FILE = os.path.join(REPO_ROOT, "local_runner", "cache", "last_audited_commit.txt")
DEP_MAP = os.path.join(REPO_ROOT, "DEPENDENCY_MAP.md")
DATA_CONTRACT = os.path.join(REPO_ROOT, "DATA_CONTRACT.md")


def git(cmd):
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + cmd, capture_output=True, text=True, cwd=REPO_ROOT
    )
    return result.stdout.strip()


def git_ok(cmd):
    """Run a git command and return True if it succeeds."""
    result = subprocess.run(
        ["git"] + cmd, capture_output=True, text=True, cwd=REPO_ROOT
    )
    return result.returncode == 0


def read_file(path):
    """Read a file, return empty string if missing."""
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def find_downstream_consumers(changed_files, depmap_content):
    """Parse DEPENDENCY_MAP.md to find downstream consumer files for changed components."""
    if not depmap_content:
        return [], []

    downstream_names = []
    downstream_files = set()

    for filepath in changed_files:
        if not filepath.endswith(".py"):
            continue

        basename = os.path.splitext(os.path.basename(filepath))[0]

        # Find the section for this component and extract downstream consumer filenames
        in_section = False
        in_downstream = False
        consumers = []

        for line in depmap_content.split("\n"):
            if line.startswith("### "):
                if basename.lower() in line.lower():
                    in_section = True
                    in_downstream = False
                else:
                    in_section = False
                    in_downstream = False
                continue

            if in_section and "Downstream Consumers" in line:
                in_downstream = True
                continue

            if in_section and in_downstream:
                if line.startswith("##") or line.startswith("---"):
                    in_downstream = False
                    continue
                # Match `filename.py` patterns
                matches = re.findall(r"`([a-z_]+\.py)`", line)
                consumers.extend(matches)

        if consumers:
            downstream_names.append(f"  {filepath} impacts: {', '.join(consumers)}")
            for consumer in consumers:
                # Skip if already in changed set
                if any(consumer in cf for cf in changed_files):
                    continue
                # Find the actual file
                for search_dir in ["local_runner", "scripts", "."]:
                    candidate = os.path.join(REPO_ROOT, search_dir, consumer)
                    if os.path.exists(candidate):
                        rel_path = os.path.join(search_dir, consumer)
                        downstream_files.add(rel_path)
                        break

    return downstream_names, list(downstream_files)


def map_to_specs(changed_files):
    """Map changed files to their spec documents."""
    changed_str = "\n".join(changed_files)
    specs = set()

    mappings = [
        (r"expr_cache_builder|vectorized_cache_builder|vectorized_dispatch|vectorized_indicators", "EXPRESSION_ENGINE_V2.md"),
        (r"scanperfect\.py", "UI_FLOW.md"),
        (r"nightly\.py", "NIGHTLY_REFRESH.md"),
        (r"nightly\.py", "LOCALIZE.md"),
        (r"seed_vault", "LOCALIZE.md"),
        (r"pyramid_grinder|signal_grinder|spiderweb", "SIGNAL_GRINDER.md"),
        (r"refinement", "REFINEMENT_GRINDER.md"),
        (r"ev_grinder|ev_tree_scorer", "EV_GRINDER.md"),
        (r"cache_builder\.py", "LOCALIZE.md"),
        (r"cache_builder\.py", "NIGHTLY_REFRESH.md"),
        (r"matrix_builder", "LOCALIZE.md"),
        (r"signal_filter|signal_exit", "PIPELINE_V2.md"),
        (r"entry_candle|entry_grinder", "ENTRY_GRINDER.md"),
        (r"profit_grinder", "PROFIT_GRINDER.md"),
        (r"market_cache_builder|fetch_missing_market", "LOCALIZE.md"),
        (r"consensus_engine", "CONSENSUS_SPEC.md"),
        (r"exit_grinder|exit_compute|exit_expressions", "PIPELINE_V2.md"),
        (r"fetch_fundamentals|fetch_universe|build_tradable", "NIGHTLY_REFRESH.md"),
        (r"lsp_detector|algo_line_detector", "EXPRESSION_ENGINE_V2.md"),
        (r"brute_expressions", "EXPRESSION_ENGINE_V2.md"),
        (r"server\.py", "DATA_CONTRACT.md"),
        (r"local_db|analysis_api", "DATA_CONTRACT.md"),
    ]

    for pattern, spec in mappings:
        if re.search(pattern, changed_str):
            specs.add(spec)

    if not specs:
        specs.add("PIPELINE_V2.md")

    return sorted(specs)


def main():
    # ── Determine diff range ──

    if os.path.exists(STATE_FILE):
        last_audited = read_file(STATE_FILE).strip()
        if not git_ok(["cat-file", "-e", last_audited]):
            print(f"  Warning: last audited commit {last_audited} no longer exists. Diffing HEAD~1.")
            last_audited = git(["rev-parse", "HEAD~1"])
    else:
        print("  First run — no audit history. Diffing HEAD~1.")
        last_audited = git(["rev-parse", "HEAD~1"])

    current = git(["rev-parse", "HEAD"])

    if last_audited == current:
        print()
        print("  Nothing to audit — HEAD is the same as last audited commit.")
        print(f"  ({current})")
        print()
        return

    n_commits = git(["rev-list", "--count", f"{last_audited}..{current}"])
    short_from = git(["rev-parse", "--short", last_audited])
    short_to = git(["rev-parse", "--short", current])

    print()
    print("=" * 60)
    print("  ScanPerfect Code Auditor")
    print(f"  Auditing {n_commits} commit(s): {short_from} → {short_to}")
    print("=" * 60)
    print()

    # ── Gather diff and changed files ──

    diff = git(["diff", last_audited, current])
    changed_str = git(["diff", "--name-only", last_audited, current])
    commit_msgs = git(["log", "--oneline", f"{last_audited}..{current}"])

    if not changed_str:
        print("  No files changed. Nothing to audit.")
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            f.write(current)
        return

    changed_files = [f for f in changed_str.split("\n") if f.strip()]

    print("  Changed files:")
    for f in changed_files:
        print(f"    {f}")
    print()

    # ── Map changed files to spec docs ──

    specs = map_to_specs(changed_files)
    print(f"  Spec docs: {', '.join(specs)}")

    # ── Read spec doc contents ──

    spec_content = ""
    for spec in specs:
        path = os.path.join(REPO_ROOT, spec)
        content = read_file(path)
        if content:
            spec_content += f"\n{'='*10} {spec} {'='*10}\n{content}\n"

    # ── Read full content of every changed file ──

    full_files = ""
    for filepath in changed_files:
        full_path = os.path.join(REPO_ROOT, filepath)
        content = read_file(full_path)
        if content:
            full_files += f"\n{'='*10} FULL FILE: {filepath} {'='*10}\n{content}\n"

    # ── Find downstream consumers ──

    depmap_content = read_file(DEP_MAP)
    downstream_names, downstream_paths = find_downstream_consumers(changed_files, depmap_content)

    downstream_files = ""
    if downstream_names:
        print()
        print("  Downstream consumers pulled:")
        for name in downstream_names:
            print(f"  {name}")
        for rel_path in downstream_paths:
            full_path = os.path.join(REPO_ROOT, rel_path)
            content = read_file(full_path)
            if content:
                downstream_files += f"\n{'='*10} DOWNSTREAM: {rel_path} {'='*10}\n{content}\n"

    # ── Read reference docs ──

    contract_content = read_file(DATA_CONTRACT)

    print()
    print("  Running audit via claude -p ...")
    print()

    # ── Build prompt ──

    prompt = f"""You are a code auditor for ScanPerfect, a quantitative swing trading screener.

YOUR ROLE: You evaluate code changes. You do NOT write code. You do NOT suggest fixes. You ONLY evaluate and report PASS or FAIL with evidence.

You have been given:
- A git diff (what changed across {n_commits} commits)
- The full content of every changed file
- The full content of downstream consumer files (files that read output from the changed files)
- The relevant specification documents
- DATA_CONTRACT.md (schemas, file formats, data flow rules)
- DEPENDENCY_MAP.md (per-component inputs, outputs, downstream consumers)

EVALUATE THESE FOUR CRITERIA:

1. PURPOSE
Read the spec doc for each changed component. What does the spec say this component should do?
Does the code change still achieve that purpose? Does it do what the spec says, or has it drifted
to do something else?

FAIL if the code no longer fulfills the purpose defined in its spec doc.

2. SPEC COMPLIANCE
Go through the spec requirements for each changed component. For each requirement, check whether
the code satisfies it:
- File paths: inputs read from and outputs written to the correct locations?
- Data flow: data coming from the right source (local cache vs network vs database)?
- Function signatures: match what callers expect?
- Worker patterns: correct executor type, correct parallelism?
- RAM management: del + gc.collect patterns present where required?
- Output formats: correct types, schemas, file extensions?
- Constants and thresholds: hardcoded values match the spec?

FAIL if ANY spec requirement is not met. Cite the specific spec requirement and what the code does differently.

3. REGRESSION SAFETY
Use DATA_CONTRACT.md and DEPENDENCY_MAP.md to check:
- If output file paths or names changed, will downstream consumers (listed in DEPENDENCY_MAP.md) find them?
- If output data format changed, will downstream consumers handle the new format?
- If function signatures changed, will callers (listed in DEPENDENCY_MAP.md) break?
- If file schemas changed, does DATA_CONTRACT.md still describe them accurately?
- Could this change cause silent wrong numbers — plausible-looking output that is actually incorrect?

You have been given the full source of downstream consumer files. CHECK THEM. Verify that the
changed code's outputs are still compatible with how downstream files read them.

FAIL if any regression risk exists. The most dangerous failure is code that produces plausible
wrong numbers with no errors.

4. CODE QUALITY
- Is there dead code, commented-out code, or unused imports?
- Is there redundant logic or copy-pasted code that should be shared?
- Are there unnecessary layers of abstraction or wrapper functions?
- Is the code efficient — no obvious performance problems?
- Would a senior engineer say this is clean and maintainable?

FAIL if there is dead code that should be cleaned up, or if the implementation is unnecessarily
complex when a simpler approach would work.

OUTPUT FORMAT:

If all four criteria pass:
PASS

If any criterion fails:
FAIL: [which criteria failed] — [one sentence why]

Examples:
PASS
FAIL: purpose — builds full engine instead of incremental append
FAIL: spec, quality — wrong ticker count, 40 lines of dead code
FAIL: regression — changes output dict keys, ev_grinder reads old keys
FAIL: quality — 3 commented-out functions, unused imports

Be thorough. A false PASS is dangerous — the project owner cannot read code and relies
entirely on this audit to catch problems. When in doubt, FAIL.

COMMITS ({n_commits}):
{commit_msgs}

CHANGED FILES:
{changed_str}

DIFF:
{diff}

FULL FILE CONTENTS:
{full_files}

DOWNSTREAM CONSUMER FILES:
{downstream_files}

DATA_CONTRACT.md:
{contract_content}

DEPENDENCY_MAP.md:
{depmap_content}

SPECIFICATION DOCUMENTS:
{spec_content}
"""

    # ── Run claude -p ──

    # Force UTF-8 encoding on Windows (avoids cp1252 errors from unicode in diffs)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        # Write prompt to temp file to avoid stdin encoding issues on Windows
        import tempfile
        prompt_file = os.path.join(REPO_ROOT, "local_runner", "cache", "_audit_prompt.txt")
        with open(prompt_file, "w", encoding="utf-8") as pf:
            pf.write(prompt)

        # shell=True needed on Windows to find claude.cmd
        # Read from file instead of stdin to avoid encoding issues
        cmd = "claude -p < " + chr(34) + prompt_file + chr(34)
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=300,
            shell=True, env=env
        )
        output = result.stdout.strip()
        output = result.stdout.strip()
    except FileNotFoundError:
        print("  ERROR: 'claude' command not found.")
        print("  Install Claude Code: npm install -g @anthropic-ai/claude-code")
        print("  Then authenticate: claude")
        return
    except subprocess.TimeoutExpired:
        print("  ERROR: Audit timed out after 5 minutes.")
        return

    if not output:
        print("  ERROR: claude -p returned empty output.")
        if result.stderr:
            print(f"  stderr: {result.stderr[:500]}")
        return

    # ── Output ──

    print()
    print("=" * 60)
    print(output)
    print("=" * 60)
    print()

    # Save result
    os.makedirs(os.path.join(REPO_ROOT, "local_runner", "cache"), exist_ok=True)
    with open(os.path.join(REPO_ROOT, "local_runner", "cache", "last_audit.txt"), "w") as f:
        f.write(output)
    print("  Audit saved to local_runner/cache/last_audit.txt")

    # ── Update last audited commit ──

    first_line = output.split("\n")[0].strip()
    if first_line.startswith("PASS"):
        with open(STATE_FILE, "w") as f:
            f.write(current)
        print(f"  Last audited commit updated to {short_to}")
    else:
        print(f"  FAIL — last audited commit NOT updated (stays at {short_from})")
        print("  Fix the issues and re-run: python audit.py")

    print()


if __name__ == "__main__":
    main()
