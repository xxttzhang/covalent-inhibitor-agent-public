"""Resume-safe AF3 worker for a frozen label-blind validation panel."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def successful_output(output_dir: Path) -> bool:
    manifest = output_dir.with_suffix(".run_manifest.json")
    if not manifest.is_file():
        return False
    try:
        payload = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("status") == "SUCCESS" and bool(
        list(output_dir.rglob("*_model.cif"))
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--panel-config", type=Path, required=True)
    parser.add_argument("--operational-root", type=Path, required=True)
    parser.add_argument("--results-root", type=Path, required=True)
    parser.add_argument("--gpu", type=int, required=True)
    parser.add_argument("--worker-index", type=int, required=True)
    parser.add_argument("--worker-count", type=int, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--num-recycles", type=int, default=3)
    parser.add_argument("--num-diffusion-samples", type=int, default=5)
    args = parser.parse_args()
    if not 0 <= args.worker_index < args.worker_count:
        raise ValueError("worker-index must be in [0, worker-count)")

    project = args.project_root.resolve()
    panel = json.loads(args.panel_config.read_text())
    queue: list[tuple[str, dict[str, object]]] = []
    preparation_failed = 0
    for target in panel["targets"]:
        target_id = str(target["target_id"])
        manifest_path = args.operational_root / target_id / "af3_inputs" / "batch_manifest.json"
        identities = json.loads(manifest_path.read_text())
        if len(identities) != int(panel["dataset"]["rows_per_target"]):
            raise ValueError(f"{target_id}: incomplete batch manifest")
        for identity in sorted(identities, key=lambda row: int(row["input_sdf_index"])):
            if str(identity.get("status", "SUCCESS")) != "SUCCESS":
                preparation_failed += 1
                continue
            queue.append((target_id, identity))
    queue = [job for position, job in enumerate(queue) if position % args.worker_count == args.worker_index]

    state_path = args.results_root / f"af3_worker_{args.worker_index}.json"
    state: dict[str, object] = {
        "status": "RUNNING",
        "gpu": args.gpu,
        "worker_index": args.worker_index,
        "worker_count": args.worker_count,
        "assigned_jobs": len(queue),
        "panel_preparation_failed": preparation_failed,
        "completed_success": 0,
        "completed_failed": 0,
        "skipped_existing": 0,
        "last_job": None,
    }
    atomic_json(state_path, state)
    runner = project / "scripts" / "run_af3_job.py"
    jax_cache = args.results_root / "af3_jax_cache"
    start = time.monotonic()
    for target_id, identity in queue:
        job_name = str(identity["job_name"])
        output_dir = args.results_root / target_id / "af3_raw" / job_name
        state["last_job"] = {"target_id": target_id, "job_name": job_name}
        if successful_output(output_dir):
            state["skipped_existing"] = int(state["skipped_existing"]) + 1
            atomic_json(state_path, state)
            continue
        command = [
            str(args.python),
            str(runner),
            "--json-path",
            str(identity["input_json"]),
            "--output-dir",
            str(output_dir),
            "--gpu",
            str(args.gpu),
            "--num-recycles",
            str(args.num_recycles),
            "--num-diffusion-samples",
            str(args.num_diffusion_samples),
            "--num-seeds",
            "1",
            "--skip-data-pipeline",
            "--jax-cache-dir",
            str(jax_cache),
        ]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(project / "src")
        completed = subprocess.run(command, cwd=project, env=environment, check=False)
        key = "completed_success" if completed.returncode == 0 and successful_output(output_dir) else "completed_failed"
        state[key] = int(state[key]) + 1
        state["elapsed_seconds"] = time.monotonic() - start
        atomic_json(state_path, state)
    state["status"] = "COMPLETE"
    state["elapsed_seconds"] = time.monotonic() - start
    atomic_json(state_path, state)
    print(json.dumps(state, indent=2))
    return 0 if int(state["completed_failed"]) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
