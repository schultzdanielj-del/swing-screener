
# PIPELINE DASHBOARD — Remote job queue for desktop agent
# ═══════════════════════════════════════════════════════════
# Added to server.py — endpoints for the pipeline dashboard
# Desktop agent polls for jobs, runs scripts locally, streams logs back
#
# Add this BEFORE the final app.mount() line in server.py

PIPELINE_FILE = os.path.join("data", "pipeline_state.json")
PIPELINE_LOGS_FILE = os.path.join("data", "pipeline_logs.json")

# Pipeline step definitions (must match agent)
PIPELINE_STEPS = [
    {"id": "nightly", "name": "0. Nightly Refresh", "category": "data",
     "description": "Append new OHLCV bars, rebuild caches and matrix.",
     "prerequisites": [], "result_files": []},
    {"id": "signal_grind", "name": "1. Signal Grinder", "category": "grind",
     "description": "Multi-pass pyramid grinder. ~5-15 min per peak target.",
     "prerequisites": [], "result_files": ["data/pyramid_results_dtss.json"]},
    {"id": "exit_grind", "name": "2. Exit Grinder", "category": "grind",
     "description": "Find best TA-driven exit condition across 6,410 expressions.",
     "prerequisites": [], "result_files": ["data/exit_grind/exit_grind_dtss.json"]},
    {"id": "multistage_exit", "name": "3. Multi-Stage Exit", "category": "grind",
     "description": "8-pass multi-stage exit search: trims, MFE gates, cross-conditions.",
     "prerequisites": [], "result_files": ["data/multistage_exit/ms_exit_dtss.json"]},
    {"id": "outcome_grind", "name": "4. Outcome Grinder", "category": "analysis",
     "description": "Apply exit condition to all pyramid signals. Classify outcomes.",
     "prerequisites": ["signal_grind", "exit_grind"],
     "result_files": ["data/outcome_grind/outcome_signals_dtss.json"]},
    {"id": "backtest", "name": "5. Backtest Runner", "category": "analysis",
     "description": "Scan full history, generate charts per signal.",
     "prerequisites": ["signal_grind"], "result_files": []},
]


def _load_pipeline_state():
    return _load_grinder_json(PIPELINE_FILE, {"steps": {}, "jobs": []})


def _save_pipeline_state(state):
    _save_grinder_json(PIPELINE_FILE, state)


def _load_pipeline_logs():
    return _load_grinder_json(PIPELINE_LOGS_FILE, {})


def _save_pipeline_logs(logs):
    _save_grinder_json(PIPELINE_LOGS_FILE, logs)


@app.get("/api/pipeline/steps")
async def get_pipeline_steps():
    """Get all pipeline steps with current state."""
    state = _load_pipeline_state()
    agent = _load_grinder_json(GRINDER_AGENT_FILE, {})

    # Check agent online status
    agent_status = "unknown"
    last_hb = agent.get("last_heartbeat", "")
    if last_hb:
        try:
            hb_time = datetime.fromisoformat(last_hb.replace('+00:00', '').replace('Z', ''))
            age = (datetime.utcnow() - hb_time).total_seconds()
            agent_status = "online" if age < 60 else "offline"
        except:
            agent_status = "unknown"

    steps_out = []
    for step_def in PIPELINE_STEPS:
        step_state = state.get("steps", {}).get(step_def["id"], {
            "status": "pending", "started_at": None, "finished_at": None,
            "duration_s": None, "exit_code": None, "error": None,
            "result_summary": None,
        })

        # Check prerequisites
        can_run = True
        if any(j.get("status") in ("queued", "running") for j in state.get("jobs", [])):
            can_run = False
        else:
            for prereq in step_def["prerequisites"]:
                prereq_state = state.get("steps", {}).get(prereq, {})
                if prereq_state.get("status") != "done":
                    can_run = False
                    break

        steps_out.append({
            **step_def,
            "state": step_state,
            "can_run": can_run,
        })

    # Find running job
    running = None
    for j in state.get("jobs", []):
        if j.get("status") in ("queued", "running"):
            running = j.get("step_id")
            break

    return {
        "steps": steps_out,
        "running": running,
        "agent_status": agent_status,
        "agent_last_heartbeat": last_hb,
    }


@app.post("/api/pipeline/run/{step_id}")
async def pipeline_run_step(step_id: str):
    """Queue a pipeline step for the desktop agent."""
    step_def = next((s for s in PIPELINE_STEPS if s["id"] == step_id), None)
    if not step_def:
        return {"error": f"Unknown step: {step_id}"}

    state = _load_pipeline_state()

    # Check not already running
    for j in state.get("jobs", []):
        if j.get("status") in ("queued", "running"):
            return {"error": f"Already running: {j.get('step_id')}"}

    # Check prerequisites
    for prereq in step_def["prerequisites"]:
        prereq_state = state.get("steps", {}).get(prereq, {})
        if prereq_state.get("status") != "done":
            return {"error": f"Prerequisite not met: {prereq}"}

    # Create job
    job = {
        "job_id": f"pipe_{step_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "step_id": step_id,
        "status": "queued",
        "created_at": datetime.now().isoformat(),
    }
    state.setdefault("jobs", []).append(job)

    # Update step state
    state.setdefault("steps", {})[step_id] = {
        "status": "queued",
        "started_at": None,
        "finished_at": None,
        "duration_s": None,
        "exit_code": None,
        "error": None,
        "result_summary": None,
    }

    _save_pipeline_state(state)

    # Clear old logs for this step
    logs = _load_pipeline_logs()
    logs[step_id] = []
    _save_pipeline_logs(logs)

    return {"status": "queued", "job_id": job["job_id"], "step_id": step_id}


@app.get("/api/pipeline/jobs/pending")
async def pipeline_pending_jobs():
    """Agent polls this to pick up queued pipeline jobs."""
    state = _load_pipeline_state()
    pending = []
    for j in state.get("jobs", []):
        if j.get("status") == "queued":
            j["status"] = "claimed"
            pending.append(j)
    if pending:
        _save_pipeline_state(state)
    return {"jobs": pending}


@app.post("/api/pipeline/status")
async def pipeline_update_status(request: Request):
    """Agent reports step status updates."""
    body = await request.json()
    step_id = body.get("step_id")
    status = body.get("status")

    state = _load_pipeline_state()

    # Update step state
    step_state = state.setdefault("steps", {}).setdefault(step_id, {})
    step_state["status"] = status

    if status == "running":
        step_state["started_at"] = body.get("timestamp", datetime.now().isoformat())
    elif status in ("done", "error", "stopped"):
        step_state["finished_at"] = body.get("timestamp", datetime.now().isoformat())
        step_state["duration_s"] = body.get("duration_s")
        step_state["exit_code"] = body.get("exit_code")
        step_state["error"] = body.get("error")
        step_state["result_summary"] = body.get("result_summary")

        # Clear job from active
        for j in state.get("jobs", []):
            if j.get("step_id") == step_id and j.get("status") in ("claimed", "running"):
                j["status"] = status

    _save_pipeline_state(state)
    return {"ok": True}


@app.post("/api/pipeline/logs")
async def pipeline_append_logs(request: Request):
    """Agent streams log lines back."""
    body = await request.json()
    step_id = body.get("step_id")
    lines = body.get("lines", [])

    logs = _load_pipeline_logs()
    existing = logs.get(step_id, [])
    existing.extend(lines)
    # Cap at 5000 lines
    if len(existing) > 5000:
        existing = existing[-4000:]
    logs[step_id] = existing
    _save_pipeline_logs(logs)
    return {"ok": True, "total_lines": len(existing)}


@app.get("/api/pipeline/logs/{step_id}")
async def pipeline_get_logs(step_id: str, after: int = 0):
    """Get log lines for a step. Use 'after' param for polling (line index)."""
    logs = _load_pipeline_logs()
    all_lines = logs.get(step_id, [])
    return {
        "step_id": step_id,
        "lines": all_lines[after:],
        "total": len(all_lines),
        "after": after,
    }


@app.post("/api/pipeline/reset/{step_id}")
async def pipeline_reset_step(step_id: str):
    """Reset a step to pending."""
    state = _load_pipeline_state()
    state.setdefault("steps", {})[step_id] = {
        "status": "pending",
        "started_at": None, "finished_at": None,
        "duration_s": None, "exit_code": None,
        "error": None, "result_summary": None,
    }
    _save_pipeline_state(state)
    return {"ok": True, "step_id": step_id}


@app.post("/api/pipeline/stop")
async def pipeline_stop():
    """Request the agent to stop the current job."""
    state = _load_pipeline_state()
    for j in state.get("jobs", []):
        if j.get("status") in ("queued", "claimed", "running"):
            j["status"] = "stop_requested"
    _save_pipeline_state(state)
    return {"ok": True}
