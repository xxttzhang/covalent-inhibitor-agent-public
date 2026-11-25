"""Worker progress is observational; only registered final evidence grants admission."""
import json
import math
import os
import time
from datetime import datetime, timezone


class MDProgress:
    def __init__(self, folder, system_id, total_steps):
        self.folder = folder
        self.system_id = system_id
        self.total_steps = total_steps
        self.started = time.monotonic()

    def report(self, stage, completed_steps=0, **metrics):
        value = dict(schema="openmm-progress-v1", system_id=self.system_id,
            stage=stage, completed_steps=completed_steps, total_steps=self.total_steps,
            elapsed_seconds=time.monotonic() - self.started,
            observed_at=datetime.now(timezone.utc).isoformat(), metrics=metrics,
            scientific_acceptance=False)
        temporary = self.folder / "progress.next.json"
        temporary.write_text(json.dumps(value, allow_nan=False) + "\n")
        os.replace(temporary, self.folder / "progress.json")
        print(json.dumps({"progress": value}, allow_nan=False), flush=True)


def read_equilibration_progress(folder, system_id):
    path = folder / "progress.json"
    if not path.exists():
        return None
    try:
        if path.is_symlink() or any(p.is_symlink() for p in path.parents) or path.stat().st_size > 16384:
            raise ValueError("Unsafe progress file")
        value = json.loads(path.read_text())
        if (value.get("schema") != "openmm-progress-v1" or value.get("system_id") != system_id
                or value.get("total_steps") != 250000 or type(value.get("completed_steps")) is not int
                or not 0 <= value["completed_steps"] <= 250000
                or value.get("scientific_acceptance") is not False
                or type(value.get("elapsed_seconds")) not in (int, float)
                or not math.isfinite(value["elapsed_seconds"]) or value["elapsed_seconds"] < 0):
            raise ValueError("Progress identity or step range is invalid")
        return {**value, "meaning": "Worker-reported progress, not liveness or scientific acceptance"}
    except (OSError, ValueError, TypeError, AttributeError) as error:
        return {"unavailable": True, "reason": str(error), "scientific_acceptance": False}
