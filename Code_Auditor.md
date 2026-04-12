# ScanPerfect Code Auditor

## What This Is

A manual code review tool with **two parallel implementations** at the repo root:
- **`audit.sh`** — Bash implementation (Git Bash / WSL / Linux / macOS).
- **`audit.py`** — Python implementation (cross-platform; the doc examples below use this).

Both do the same thing, read the same state file (`local_runner/cache/last_audited_commit.txt`), and have near-identical behavior. Having two implementations is tech debt — the next cleanup round should consolidate on one or document the intended split. For now, use whichever fits your shell.

When you're ready to check your work, run the auditor from any terminal. It uses Claude Code in pipe mode (`claude -p`) to read the diff, the relevant spec docs, the dependency map, and downstream consumer files, then evaluates four criteria:

1. **PURPOSE** — Does the code still do what the component's spec doc says it should do?
2. **SPEC COMPLIANCE** — Is it built according to the spec? File paths, data formats, function signatures, worker patterns, RAM management, constants.
3. **REGRESSION SAFETY** — Does it break anything in DATA_CONTRACT.md or DEPENDENCY_MAP.md? Are downstream consumer files still compatible?
4. **CODE QUALITY** — No dead code, no commented-out code, no redundant logic. Clean and efficient.

Runs on your local machine. No extra cost — uses your Claude Max subscription through Claude Code.

---

## How It Works

1. Looks up the last audited commit (stored in `local_runner/cache/last_audited_commit.txt`)
2. Diffs from that commit to current HEAD — catches everything since last audit
3. Finds which files changed
4. Maps changed files to their spec docs
5. Reads DEPENDENCY_MAP.md to find downstream consumers of every changed file
6. Pulls full source of changed files AND downstream consumer files
7. Sends everything to `claude -p` for evaluation
8. On PASS: saves current HEAD as the new last audited commit
9. On FAIL: does NOT update — next run will re-audit the same block

---

## Prerequisites

### Claude Code
```
npm install -g @anthropic-ai/claude-code
```
Authenticate once:
```
claude
```
Log in via browser, then close.

---

## Usage

From any terminal (Command Prompt, PowerShell, or Git Bash) in the repo directory:

```
python audit.py          # audit just the last commit (default)
python audit.py --all    # audit everything since last audit
```

Default mode audits the most recent commit only — fast, cheap, use it on anything you're not sure about.

Batch mode (`--all`) diffs from the last audited commit to HEAD. Use it after a big block of work to catch everything at once.

### After Claude pushes code from chat
Pull first, then audit:
```
git pull
python audit.py
```

### If it FAILs
The auditor does NOT advance its bookmark on FAIL. Fix the issues, commit, and run `python audit.py` again.

### Force re-audit from scratch
Delete the state file:
```
del local_runner\cache\last_audited_commit.txt
python audit.py
```

---

## What It Looks Like

```
============================================================
  ScanPerfect Code Auditor
  Auditing 3 commit(s): a1b2c3d → e4f5g6h
============================================================

  Changed files:
    scripts/ev_grinder.py
    local_runner/pyramid_grinder.py

  Spec docs: EV_GRINDER.md, SIGNAL_GRINDER.md

  Downstream consumers pulled:
    ev_grinder.py impacts: profit_grinder.py, scanperfect.py

  Running audit via claude -p ...

============================================================
PASS
============================================================

  Audit saved to local_runner/cache/last_audit.txt
  Last audited commit updated to e4f5g6h
```

```
============================================================
FAIL: regression — ev_grinder output changed 'wr_predicted' key to 'predicted_wr',
profit_grinder.py line 142 reads 'wr_predicted' and will KeyError
============================================================

  Audit saved to local_runner/cache/last_audit.txt
  FAIL — last audited commit NOT updated (stays at a1b2c3d)
  Fix the issues and re-run: python audit.py
```

---

## Maintenance

**Spec mapping:** The mapping in `audit.py` connects changed filenames to spec docs. When you add a new component or spec doc, add a line to the mapping.

**Dependency map:** The auditor reads `DEPENDENCY_MAP.md` at runtime. When components are added or dependencies change, update the dependency map — the auditor picks it up automatically.

**State file:** `local_runner/cache/last_audited_commit.txt` — one line, a git commit hash. Safe to delete if you want to reset.
