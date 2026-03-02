"""
Pipeline Dashboard — Local web UI for running the swing screener pipeline.

Usage:
    cd swing-screener
    python dashboard/server.py

Then open http://localhost:8080 in your browser.

Each pipeline step:
  - Shows status (pending / running / done / error)
  - Has a RUN button (only enabled when prerequisites are met)
  - Streams live logs while running
  - Shows results summary when done
  - Can be re-run at any time

State persists in dashboard/state.json so you can close and reopen.
"""

import asyncio
import json
import os
import sys
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ============================================================
# Paths
# ============================================================
DASHBOARD_DIR = Path(__file__).parent
PROJECT_ROOT = DASHBOARD_DIR.parent
STATE_FILE = DASHBOARD_DIR / "state.json"
LOG_DIR = DASHBOARD_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ============================================================
# Pipeline Definition
# ============================================================
# Each step: id, name, description, command, prerequisites, result_check
PIPELINE_STEPS = [
    {
        "id": "nightly",
        "name": "0. Nightly Refresh",
        "description": "Append new OHLCV bars from Railway, rebuild caches and matrix. Run after market close (~4:30pm ET).",
        "command": [sys.executable, str(PROJECT_ROOT / "local_runner" / "nightly.py")],
        "prerequisites": [],
        "category": "data",
        "result_files": [],
        "rerunnable": True,
    },
    {
        "id": "signal_grind",
        "name": "1. Signal Grinder",
        "description": "Multi-pass pyramid grinder. Finds conditions that separate examples from universe. ~5-15 min per peak target.",
        "command": [
            sys.executable, str(PROJECT_ROOT / "local_runner" / "pyramid_grinder.py"),
            "--setup", "dtss", "--peak-target", "3", "--beam", "10000", "--depth", "100"
        ],
        "prerequisites": [],
        "category": "grind",
        "result_files": ["data/pyramid_results_dtss.json"],
        "rerunnable": True,
    },
    {
        "id": "exit_grind",
        "name": "2. Exit Grinder (Single-Stage)",
        "description": "Find best TA-driven exit condition across 6,410 expressions. Tests against all validated examples.",
        "command": [
            sys.executable, str(PROJECT_ROOT / "scripts" / "exit_grinder.py"),
            "--setup", "dtss", "--max-forward", "120"
        ],
        "prerequisites": [],
        "category": "grind",
        "result_files": ["data/exit_grind/exit_grind_dtss.json"],
        "rerunnable": True,
    },
    {
        "id": "multistage_exit",
        "name": "3. Exit Grinder (Multi-Stage)",
        "description": "8-pass search over multi-stage exit structures: partial trims, MFE gates, cross-conditions, 3-stage combos.",
        "command": [
            sys.executable, str(PROJECT_ROOT / "scripts" / "multistage_exit_grinder.py"),
            "--setup", "dtss"
        ],
        "prerequisites": [],
        "category": "grind",
        "result_files": ["data/multistage_exit/ms_exit_dtss.json"],
        "rerunnable": True,
    },
    {
        "id": "outcome_grind",
        "name": "4. Outcome Grinder",
        "description": "Apply exit condition to all pyramid signals. Classify outcomes by move size. Requires signal + exit results.",
        "command": [
            sys.executable, str(PROJECT_ROOT / "scripts" / "outcome_grinder.py"),
            "--setup", "dtss"
        ],
        "prerequisites": ["signal_grind", "exit_grind"],
        "category": "analysis",
        "result_files": ["data/outcome_grind/outcome_signals_dtss.json"],
        "rerunnable": True,
    },
    {
        "id": "backtest",
        "name": "5. Backtest Runner",
        "description": "Scan full 5yr history with signal conditions, generate charts per signal, upload to Railway for gallery view.",
        "command": [
            sys.executable, str(PROJECT_ROOT / "scripts" / "backtest_runner.py"),
            "--setup", "dtss"
        ],
        "prerequisites": ["signal_grind"],
        "category": "analysis",
        "result_files": [],
        "rerunnable": True,
    },
]

# ============================================================
# State Management
# ============================================================
def load_state() -> dict:
    """Load persistent state from JSON file."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"steps": {}, "started_at": None}


def save_state(state: dict):
    """Save state to JSON file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def get_step_state(state: dict, step_id: str) -> dict:
    """Get state for a specific step."""
    return state.get("steps", {}).get(step_id, {
        "status": "pending",
        "started_at": None,
        "finished_at": None,
        "duration_s": None,
        "exit_code": None,
        "error": None,
        "result_summary": None,
    })


def check_result_files(step: dict) -> dict:
    """Check if result files exist and return info."""
    results = {}
    for rf in step.get("result_files", []):
        full_path = PROJECT_ROOT / rf
        if full_path.exists():
            stat = full_path.stat()
            results[rf] = {
                "exists": True,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
        else:
            results[rf] = {"exists": False}
    return results


# ============================================================
# Process Runner
# ============================================================
class ProcessManager:
    """Manages running pipeline steps as subprocesses."""

    def __init__(self):
        self.running: Optional[dict] = None  # {step_id, process, log_path}
        self.websockets: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def broadcast(self, msg: dict):
        """Send message to all connected websockets."""
        dead = []
        for ws in self.websockets:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.websockets.remove(ws)

    async def run_step(self, step_id: str):
        """Run a pipeline step as a subprocess."""
        async with self._lock:
            if self.running:
                return {"error": f"Already running: {self.running['step_id']}"}

            step = next((s for s in PIPELINE_STEPS if s["id"] == step_id), None)
            if not step:
                return {"error": f"Unknown step: {step_id}"}

            # Set up logging
            log_path = LOG_DIR / f"{step_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            latest_log = LOG_DIR / f"{step_id}_latest.log"

            state = load_state()
            step_state = {
                "status": "running",
                "started_at": datetime.now().isoformat(),
                "finished_at": None,
                "duration_s": None,
                "exit_code": None,
                "error": None,
                "result_summary": None,
            }
            state.setdefault("steps", {})[step_id] = step_state
            save_state(state)

            self.running = {"step_id": step_id, "log_path": str(log_path)}

        await self.broadcast({"type": "step_status", "step_id": step_id, "status": "running"})

        # Run subprocess in background
        asyncio.create_task(self._run_subprocess(step, log_path, latest_log))
        return {"status": "started", "step_id": step_id}

    async def _run_subprocess(self, step: dict, log_path: Path, latest_log: Path):
        """Execute the subprocess and stream output."""
        step_id = step["id"]
        start_time = time.time()

        try:
            process = await asyncio.create_subprocess_exec(
                *step["command"],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(PROJECT_ROOT),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )

            with open(log_path, "w") as lf, open(latest_log, "w") as latest_lf:
                async for line in process.stdout:
                    text = line.decode("utf-8", errors="replace")
                    lf.write(text)
                    lf.flush()
                    latest_lf.write(text)
                    latest_lf.flush()
                    await self.broadcast({
                        "type": "log",
                        "step_id": step_id,
                        "text": text,
                    })

            await process.wait()
            exit_code = process.returncode
            duration = round(time.time() - start_time, 1)

            # Update state
            state = load_state()
            step_state = state.get("steps", {}).get(step_id, {})
            step_state["status"] = "done" if exit_code == 0 else "error"
            step_state["finished_at"] = datetime.now().isoformat()
            step_state["duration_s"] = duration
            step_state["exit_code"] = exit_code

            if exit_code != 0:
                # Read last 20 lines for error summary
                try:
                    with open(log_path) as lf:
                        lines = lf.readlines()
                        step_state["error"] = "".join(lines[-20:])
                except Exception:
                    step_state["error"] = f"Exit code {exit_code}"

            # Check result files
            result_info = check_result_files(step)
            step_state["result_files"] = result_info

            # Try to extract result summary from output
            step_state["result_summary"] = self._extract_summary(step_id, log_path)

            state["steps"][step_id] = step_state
            save_state(state)

            await self.broadcast({
                "type": "step_status",
                "step_id": step_id,
                "status": step_state["status"],
                "duration_s": duration,
                "exit_code": exit_code,
                "result_summary": step_state.get("result_summary"),
            })

        except Exception as e:
            state = load_state()
            step_state = state.get("steps", {}).get(step_id, {})
            step_state["status"] = "error"
            step_state["error"] = str(e)
            step_state["finished_at"] = datetime.now().isoformat()
            state["steps"][step_id] = step_state
            save_state(state)

            await self.broadcast({
                "type": "step_status",
                "step_id": step_id,
                "status": "error",
                "error": str(e),
            })

        finally:
            async with self._lock:
                self.running = None

    def _extract_summary(self, step_id: str, log_path: Path) -> Optional[str]:
        """Try to extract a meaningful summary from the log output."""
        try:
            with open(log_path) as f:
                lines = f.readlines()

            # Look for common result patterns
            summary_lines = []
            capture = False
            for line in lines[-50:]:  # Last 50 lines
                lower = line.lower().strip()
                if any(kw in lower for kw in [
                    "winner", "best:", "result:", "final", "complete",
                    "signals", "peak", "conditions", "floor=", "median=",
                    "all passes complete", "total signals",
                ]):
                    summary_lines.append(line.strip())

            return "\n".join(summary_lines[-10:]) if summary_lines else None
        except Exception:
            return None

    async def stop_step(self):
        """Kill the running subprocess."""
        async with self._lock:
            if not self.running:
                return {"error": "Nothing running"}
            step_id = self.running["step_id"]

        # Find and kill the process
        # We'll update state on next status check
        try:
            import signal as sig
            # Kill by looking for the process
            result = subprocess.run(
                ["pkill", "-f", step_id],
                capture_output=True, timeout=5
            )
        except Exception:
            pass

        state = load_state()
        step_state = state.get("steps", {}).get(step_id, {})
        step_state["status"] = "stopped"
        step_state["finished_at"] = datetime.now().isoformat()
        state["steps"][step_id] = step_state
        save_state(state)

        async with self._lock:
            self.running = None

        await self.broadcast({
            "type": "step_status",
            "step_id": step_id,
            "status": "stopped",
        })

        return {"status": "stopped", "step_id": step_id}


# ============================================================
# FastAPI App
# ============================================================
app = FastAPI(title="Swing Screener Pipeline Dashboard")
pm = ProcessManager()


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the dashboard HTML."""
    html_path = DASHBOARD_DIR / "index.html"
    with open(html_path) as f:
        return f.read()


@app.get("/api/pipeline")
async def get_pipeline():
    """Get full pipeline state."""
    state = load_state()
    steps = []
    for step in PIPELINE_STEPS:
        step_state = get_step_state(state, step["id"])
        result_files = check_result_files(step)
        steps.append({
            **step,
            "command_str": " ".join(step["command"]),
            "state": step_state,
            "result_files_status": result_files,
            "can_run": _can_run(step, state),
        })

    return {
        "steps": steps,
        "running": pm.running["step_id"] if pm.running else None,
    }


def _can_run(step: dict, state: dict) -> bool:
    """Check if a step can run (prerequisites met)."""
    if pm.running:
        return False
    for prereq in step.get("prerequisites", []):
        prereq_state = get_step_state(state, prereq)
        if prereq_state.get("status") != "done":
            return False
    return True


@app.post("/api/run/{step_id}")
async def run_step(step_id: str):
    """Start running a pipeline step."""
    result = await pm.run_step(step_id)
    return result


@app.post("/api/stop")
async def stop_step():
    """Stop the running step."""
    result = await pm.stop_step()
    return result


@app.post("/api/reset/{step_id}")
async def reset_step(step_id: str):
    """Reset a step to pending."""
    state = load_state()
    if step_id in state.get("steps", {}):
        state["steps"][step_id] = {
            "status": "pending",
            "started_at": None,
            "finished_at": None,
            "duration_s": None,
            "exit_code": None,
            "error": None,
            "result_summary": None,
        }
        save_state(state)
    return {"status": "reset", "step_id": step_id}


@app.get("/api/logs/{step_id}")
async def get_logs(step_id: str, tail: int = 200):
    """Get recent log lines for a step."""
    log_path = LOG_DIR / f"{step_id}_latest.log"
    if not log_path.exists():
        return {"lines": [], "exists": False}

    with open(log_path) as f:
        lines = f.readlines()

    return {
        "lines": lines[-tail:],
        "total_lines": len(lines),
        "exists": True,
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for live log streaming."""
    await websocket.accept()
    pm.websockets.append(websocket)
    try:
        while True:
            # Keep connection alive, handle client messages
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        if websocket in pm.websockets:
            pm.websockets.remove(websocket)


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"  SWING SCREENER — Pipeline Dashboard")
    print(f"  http://localhost:8080")
    print(f"{'='*60}\n")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
