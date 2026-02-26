"""
Grind Storage — Standardized file management for all grinder outputs.

Structure:
    local_runner/grinds/{setup}/{step}/{step}_{timestamp}.json
    local_runner/grinds/{setup}/{step}/latest.json  (copy of newest)

Steps: signal, exit, outcome, market

Usage:
    from local_runner.grind_storage import GrindStorage

    gs = GrindStorage("dtss")

    # Save a grind result (auto-timestamps, updates latest.json)
    gs.save("signal", data_dict)

    # Load latest result for a step
    data = gs.load("signal")                # loads latest.json
    data = gs.load("signal", "20260226_104240")  # loads specific run

    # List all runs for a step
    runs = gs.list_runs("signal")  # ["20260226_104240", "20260225_093012"]

    # Get path without loading
    path = gs.path("signal")         # path to latest.json
"""

import os
import json
import shutil
import glob
from datetime import datetime

# Base directory relative to this file (local_runner/grind_storage.py -> local_runner/grinds/)
GRINDS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grinds")

VALID_STEPS = ("signal", "exit", "outcome", "market")


class GrindStorage:
    """Manage grind result files for a setup type."""

    def __init__(self, setup: str, root: str = None):
        self.setup = setup.lower()
        self.root = root or GRINDS_ROOT

    def _step_dir(self, step: str) -> str:
        """Get directory for a step, creating if needed."""
        if step not in VALID_STEPS:
            raise ValueError(f"Invalid step '{step}'. Must be one of: {VALID_STEPS}")
        d = os.path.join(self.root, self.setup, step)
        os.makedirs(d, exist_ok=True)
        return d

    def save(self, step: str, data: dict, timestamp: str = None) -> str:
        """Save grind result with timestamp. Updates latest.json.

        Returns path to saved file.
        """
        d = self._step_dir(step)

        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        filename = f"{step}_{timestamp}.json"
        filepath = os.path.join(d, filename)

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, default=_json_convert)

        # Update latest.json (copy, not symlink — Windows compatible)
        latest_path = os.path.join(d, "latest.json")
        shutil.copy2(filepath, latest_path)

        print(f"  Saved: {filepath}")
        print(f"  Updated: {latest_path}")
        return filepath

    def load(self, step: str, run_id: str = None) -> dict:
        """Load grind result. None = latest."""
        d = self._step_dir(step)

        if run_id:
            filepath = os.path.join(d, f"{step}_{run_id}.json")
        else:
            filepath = os.path.join(d, "latest.json")

        if not os.path.exists(filepath):
            available = self.list_runs(step)
            if available:
                raise FileNotFoundError(
                    f"No {'latest' if not run_id else run_id} for {self.setup}/{step}. "
                    f"Available runs: {available}")
            else:
                raise FileNotFoundError(
                    f"No {step} grind results for setup '{self.setup}'. "
                    f"Run the {step} grinder first.")

        with open(filepath) as f:
            data = json.load(f)

        print(f"  Loaded: {filepath}")
        return data

    def path(self, step: str, run_id: str = None) -> str:
        """Get file path without loading."""
        d = self._step_dir(step)
        if run_id:
            return os.path.join(d, f"{step}_{run_id}.json")
        return os.path.join(d, "latest.json")

    def list_runs(self, step: str) -> list:
        """List all run timestamps for a step, newest first."""
        d = self._step_dir(step)
        pattern = os.path.join(d, f"{step}_*.json")
        files = glob.glob(pattern)

        timestamps = []
        prefix = f"{step}_"
        for f in files:
            basename = os.path.basename(f)
            if basename.startswith(prefix) and basename != "latest.json":
                ts = basename[len(prefix):-5]
                timestamps.append(ts)

        return sorted(timestamps, reverse=True)

    def has(self, step: str) -> bool:
        """Check if latest results exist for a step."""
        return os.path.exists(os.path.join(self._step_dir(step), "latest.json"))

    def summary(self) -> str:
        """Print summary of all available grinds for this setup."""
        lines = [f"Grind storage for {self.setup.upper()}:"]
        for step in VALID_STEPS:
            runs = self.list_runs(step)
            has_latest = self.has(step)
            if runs:
                lines.append(f"  {step:10s}: {len(runs)} runs, latest={'yes' if has_latest else 'no'} ({runs[0]})")
            else:
                lines.append(f"  {step:10s}: no runs")
        return "\n".join(lines)


def _json_convert(obj):
    """JSON serializer for numpy types."""
    import numpy as np
    if isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
