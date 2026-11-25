from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from covscreen.adapters.carsidock import CarsiDockAdapter
from covscreen.carsidock_results import audit_carsidock_outputs
from covscreen.jobs import sha256_file


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=int, required=True)
    ap.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    ap.add_argument("--pdb", required=True)
    ap.add_argument("--reference-ligand", required=True)
    ap.add_argument("--ligand-sdf", required=True)
    ap.add_argument("--covres", required=True)
    ap.add_argument("--reaction-type", default="Michael Addition")
    ap.add_argument("--enforce-electrophile-map", action="store_true")
    ap.add_argument("--output", required=True)
    ap.add_argument("--num-conformer", type=int, default=1)
    ap.add_argument("--cuda-device-index", type=int, default=0)
    ap.add_argument("--seed", type=int, default=20260725)
    ap.add_argument(
        "--carsidock-repo",
        required=True,
    )
    ap.add_argument(
        "--checkpoint",
        required=True,
    )
    args = ap.parse_args()
    adapter = CarsiDockAdapter(args.carsidock_repo, args.checkpoint, args.device)
    availability = adapter.check_availability()
    print(json.dumps(availability, indent=2))
    output_prefix = Path(args.output)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    run_id = f"BTK-mini_chunk_{args.chunk:04d}"
    manifest_path = output_prefix.with_suffix(".run_manifest.json")
    command = adapter.build_command(
        args.pdb, args.reference_ligand, args.ligand_sdf, args.covres,
        args.output, args.device, args.num_conformer, args.cuda_device_index,
        args.seed,
        args.reaction_type,
    )
    command[0] = sys.executable
    if args.enforce_electrophile_map:
        command = [sys.executable, str(Path(__file__).with_name("run_carsidock_guarded.py")),
                   "--carsidock-repo", str(adapter.repo), "--", *command[2:]]
    manifest = {
        "run_id": run_id,
        "engine": adapter.name,
        "chunk": args.chunk,
        "device": args.device,
        "cuda_device_index": args.cuda_device_index,
        "input_sdf": args.ligand_sdf,
        "reaction_type": args.reaction_type,
        "enforce_electrophile_map": args.enforce_electrophile_map,
        "output_prefix": args.output,
        "command": command,
        "availability": availability,
        "status": "UNAVAILABLE",
    }
    if not availability["ready"]:
        manifest["failure_reason"] = "CarsiDock-Cov or RTMScore checkpoint/torch unavailable"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        raise SystemExit("CarsiDock-Cov is not ready; do not fabricate results")
    input_paths = [Path(value).resolve() for value in
                   (args.pdb, args.reference_ligand, args.ligand_sdf)]
    manifest["input_sha256"] = {str(path): sha256_file(path) for path in input_paths}
    manifest["status"] = "RUNNING"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print("COMMAND", " ".join(command))
    stdout_path = output_prefix.with_suffix(".stdout.log")
    stderr_path = output_prefix.with_suffix(".stderr.log")
    start = time.monotonic()
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        proc = subprocess.run(command, cwd=adapter.repo, stdout=stdout, stderr=stderr, check=False)
    manifest["runtime_seconds"] = time.monotonic() - start
    manifest["return_code"] = proc.returncode
    manifest["stdout_file"] = str(stdout_path)
    manifest["stderr_file"] = str(stderr_path)
    audit = audit_carsidock_outputs(output_prefix, args.ligand_sdf)
    manifest["output_audit"] = audit
    manifest["status"] = audit["status"]
    manifest["input_unchanged"] = all(
        path.is_file() and sha256_file(path) == manifest["input_sha256"][str(path)]
        for path in input_paths
    )
    if not manifest["input_unchanged"]:
        manifest["status"] = "FAILED"
        manifest["failure_reason"] = "Input files changed during execution"
    if proc.returncode != 0:
        manifest["status"] = "FAILED"
        manifest["failure_reason"] = (
            "Upstream process returned nonzero; inspect stderr and stage audit"
        )
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return 0 if manifest["status"] == "SUCCESS" else (proc.returncode or 1)


if __name__ == "__main__":
    raise SystemExit(main())
